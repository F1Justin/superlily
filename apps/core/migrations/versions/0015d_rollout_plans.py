"""增加 Git-bound 精确 rollout plan authority 与运行时计数。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015d_rollout_plans"
down_revision: str | None = "0015c_provider_quarantine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_rollout_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("rollback_mode", sa.String(length=32), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_invocations", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_commit", sa.String(length=64), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewer", sa.String(length=256), nullable=False),
        sa.Column("canonical_json", sa.LargeBinary(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("import_outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "imported_at",
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
        sa.CheckConstraint("mode IN ('canary')", name="ck_tool_rollout_plan_mode"),
        sa.CheckConstraint(
            "rollback_mode IN ('ledger_only')",
            name="ck_tool_rollout_plan_rollback_mode",
        ),
        sa.CheckConstraint("review_status IN ('reviewed')", name="ck_tool_rollout_plan_review"),
        sa.CheckConstraint(
            "lifecycle IN ('reviewed', 'active', 'paused')",
            name="ck_tool_rollout_plan_lifecycle",
        ),
        sa.CheckConstraint("resource_version >= 1", name="ck_tool_rollout_plan_version"),
        sa.CheckConstraint(
            "max_invocations >= 1 AND max_invocations <= 1000",
            name="ck_tool_rollout_plan_max_invocations",
        ),
        sa.CheckConstraint("expires_at > starts_at", name="ck_tool_rollout_plan_window"),
        sa.CheckConstraint("import_outcome IN ('accepted')", name="ck_tool_rollout_plan_import"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "version", name="uq_tool_rollout_plan_identity"),
        sa.UniqueConstraint("plan_hash", name="uq_tool_rollout_plan_hash"),
    )
    op.create_index(
        "ix_tool_rollout_plans_lifecycle", "tool_rollout_plans", ["lifecycle"]
    )
    op.create_index(
        "ix_tool_rollout_plans_window", "tool_rollout_plans", ["starts_at", "expires_at"]
    )
    op.create_index(
        "uq_tool_rollout_single_active",
        "tool_rollout_plans",
        ["lifecycle"],
        unique=True,
        sqlite_where=sa.text("lifecycle = 'active'"),
        postgresql_where=sa.text("lifecycle = 'active'"),
    )

    op.create_table(
        "tool_rollout_plan_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_record_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("tool_id", sa.String(length=128), nullable=False),
        sa.Column("descriptor_version", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_conversation", sa.String(length=512), nullable=False),
        sa.Column("caller", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("expected_descriptor_resource_version", sa.Integer(), nullable=False),
        sa.Column("expected_provider_resource_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("caller IN ('command', 'admin_api')", name="ck_rollout_item_caller"),
        sa.CheckConstraint(
            "expected_descriptor_resource_version >= 1",
            name="ck_rollout_item_descriptor_version",
        ),
        sa.CheckConstraint(
            "expected_provider_resource_version >= 1",
            name="ck_rollout_item_provider_version",
        ),
        sa.ForeignKeyConstraint(
            ["plan_record_id"], ["tool_rollout_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_record_id", "item_id", name="uq_tool_rollout_plan_item_id"),
        sa.UniqueConstraint(
            "plan_record_id",
            "tool_id",
            "descriptor_version",
            "descriptor_hash",
            "canonical_conversation",
            "caller",
            name="uq_tool_rollout_plan_execution_target",
        ),
    )
    op.create_index(
        "ix_tool_rollout_items_tool",
        "tool_rollout_plan_items",
        ["tool_id", "descriptor_version"],
    )
    op.create_index(
        "ix_tool_rollout_items_provider", "tool_rollout_plan_items", ["provider_id"]
    )

    op.create_table(
        "tool_rollout_plan_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_record_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_lifecycle", sa.String(length=32), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle IN ('reviewed', 'active', 'paused')",
            name="ck_tool_rollout_plan_event_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["plan_record_id"], ["tool_rollout_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_record_id",
            "sequence",
            name="uq_tool_rollout_plan_lifecycle_sequence",
        ),
    )
    op.create_index(
        "ix_tool_rollout_plan_events_created",
        "tool_rollout_plan_lifecycle_events",
        ["plan_record_id", "created_at"],
    )

    op.create_table(
        "tool_rollout_plan_counters",
        sa.Column("plan_record_id", sa.String(length=36), nullable=False),
        sa.Column(
            "consumed_invocations", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "consumed_invocations >= 0",
            name="ck_tool_rollout_plan_counter_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["plan_record_id"], ["tool_rollout_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("plan_record_id"),
    )

    with op.batch_alter_table("tool_invocations") as batch:
        batch.add_column(sa.Column("rollout_plan_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("rollout_plan_item_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_tool_invocations_rollout_plan",
            "tool_rollout_plans",
            ["rollout_plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_tool_invocations_rollout_plan_item",
            "tool_rollout_plan_items",
            ["rollout_plan_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_tool_invocations_rollout_plan", ["rollout_plan_id", "created_at"]
        )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _install_sqlite_triggers()
    elif dialect == "postgresql":
        _install_postgres_triggers()


def _install_sqlite_triggers() -> None:
    statements = [
        """
        CREATE TRIGGER tool_rollout_plans_authority_no_update
        BEFORE UPDATE OF plan_id, version, plan_hash, schema_version, mode, rollback_mode,
            review_status, starts_at, expires_at, max_invocations, reason, source_commit,
            bundle_hash, reviewer, canonical_json, plan_json, import_outcome, imported_at
        ON tool_rollout_plans
        BEGIN
            SELECT RAISE(ABORT, 'rollout plan authority is immutable');
        END
        """,
        """
        CREATE TRIGGER tool_rollout_plans_no_delete
        BEFORE DELETE ON tool_rollout_plans
        BEGIN
            SELECT RAISE(ABORT, 'rollout plan authority is immutable');
        END
        """,
        """
        CREATE TRIGGER tool_rollout_plans_lifecycle_guard
        BEFORE UPDATE OF lifecycle, resource_version, updated_at ON tool_rollout_plans
        WHEN NEW.lifecycle != OLD.lifecycle
          OR NEW.resource_version != OLD.resource_version
          OR NEW.updated_at != OLD.updated_at
        BEGIN
            SELECT CASE WHEN NEW.lifecycle = OLD.lifecycle
                THEN RAISE(ABORT, 'rollout plan lifecycle change is required') END;
            SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                THEN RAISE(ABORT, 'rollout plan resource version must increase by one') END;
            SELECT CASE WHEN NOT (
                (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                (OLD.lifecycle = 'active' AND NEW.lifecycle = 'paused') OR
                (OLD.lifecycle = 'paused' AND NEW.lifecycle = 'active')
            ) THEN RAISE(ABORT, 'rollout plan lifecycle transition is not allowed') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM tool_rollout_plan_lifecycle_events
                WHERE plan_record_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_lifecycle = OLD.lifecycle
                  AND lifecycle = NEW.lifecycle
            ) THEN RAISE(ABORT, 'rollout plan lifecycle event is required') END;
        END
        """,
    ]
    for table in ("tool_rollout_plan_items", "tool_rollout_plan_lifecycle_events"):
        statements.extend(
            [
                f"""
                CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'rollout plan evidence is append-only');
                END
                """,
                f"""
                CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'rollout plan evidence is append-only');
                END
                """,
            ]
        )
    statements.extend(
        [
            """
            CREATE TRIGGER tool_rollout_plan_counters_update_guard
            BEFORE UPDATE ON tool_rollout_plan_counters
            BEGIN
                SELECT CASE WHEN NEW.plan_record_id != OLD.plan_record_id
                    THEN RAISE(ABORT, 'rollout plan counter identity is immutable') END;
                SELECT CASE WHEN NEW.consumed_invocations != OLD.consumed_invocations + 1
                    THEN RAISE(ABORT, 'rollout plan counter must increase by one') END;
                SELECT CASE WHEN NEW.last_consumed_at IS NULL
                    THEN RAISE(ABORT, 'rollout plan consumption time is required') END;
            END
            """,
            """
            CREATE TRIGGER tool_rollout_plan_counters_no_delete
            BEFORE DELETE ON tool_rollout_plan_counters
            BEGIN
                SELECT RAISE(ABORT, 'rollout plan counter cannot be deleted');
            END
            """,
        ]
    )
    for statement in statements:
        op.execute(statement)


def _install_postgres_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_tool_rollout_plan_authority_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'rollout plan authority is immutable';
            END IF;
            IF NEW.plan_id IS DISTINCT FROM OLD.plan_id
               OR NEW.version IS DISTINCT FROM OLD.version
               OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
               OR NEW.mode IS DISTINCT FROM OLD.mode
               OR NEW.rollback_mode IS DISTINCT FROM OLD.rollback_mode
               OR NEW.review_status IS DISTINCT FROM OLD.review_status
               OR NEW.starts_at IS DISTINCT FROM OLD.starts_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.max_invocations IS DISTINCT FROM OLD.max_invocations
               OR NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.source_commit IS DISTINCT FROM OLD.source_commit
               OR NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
               OR NEW.reviewer IS DISTINCT FROM OLD.reviewer
               OR NEW.canonical_json IS DISTINCT FROM OLD.canonical_json
               OR NEW.plan_json::text IS DISTINCT FROM OLD.plan_json::text
               OR NEW.import_outcome IS DISTINCT FROM OLD.import_outcome
               OR NEW.imported_at IS DISTINCT FROM OLD.imported_at THEN
                RAISE EXCEPTION 'rollout plan authority is immutable';
            END IF;
            IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
               OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
               OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
                IF NEW.lifecycle IS NOT DISTINCT FROM OLD.lifecycle THEN
                    RAISE EXCEPTION 'rollout plan lifecycle change is required';
                END IF;
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'rollout plan resource version must increase by one';
                END IF;
                IF NOT (
                    (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                    (OLD.lifecycle = 'active' AND NEW.lifecycle = 'paused') OR
                    (OLD.lifecycle = 'paused' AND NEW.lifecycle = 'active')
                ) THEN
                    RAISE EXCEPTION 'rollout plan lifecycle transition is not allowed';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM tool_rollout_plan_lifecycle_events
                    WHERE plan_record_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_lifecycle = OLD.lifecycle
                      AND lifecycle = NEW.lifecycle
                ) THEN
                    RAISE EXCEPTION 'rollout plan lifecycle event is required';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_rollout_plans_authority_guard
        BEFORE UPDATE OR DELETE ON tool_rollout_plans
        FOR EACH ROW EXECUTE FUNCTION guard_tool_rollout_plan_authority_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_rollout_plan_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'rollout plan evidence is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("tool_rollout_plan_items", "tool_rollout_plan_lifecycle_events"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_mutation
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_rollout_plan_evidence_mutation()
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_tool_rollout_plan_counter_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'rollout plan counter cannot be deleted';
            END IF;
            IF NEW.plan_record_id IS DISTINCT FROM OLD.plan_record_id THEN
                RAISE EXCEPTION 'rollout plan counter identity is immutable';
            END IF;
            IF NEW.consumed_invocations != OLD.consumed_invocations + 1 THEN
                RAISE EXCEPTION 'rollout plan counter must increase by one';
            END IF;
            IF NEW.last_consumed_at IS NULL THEN
                RAISE EXCEPTION 'rollout plan consumption time is required';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_rollout_plan_counters_guard
        BEFORE UPDATE OR DELETE ON tool_rollout_plan_counters
        FOR EACH ROW EXECUTE FUNCTION guard_tool_rollout_plan_counter_mutation()
        """
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_tool_rollout_plan_counter_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS reject_rollout_plan_evidence_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS guard_tool_rollout_plan_authority_mutation() CASCADE")
    with op.batch_alter_table("tool_invocations") as batch:
        batch.drop_index("ix_tool_invocations_rollout_plan")
        batch.drop_constraint("fk_tool_invocations_rollout_plan_item", type_="foreignkey")
        batch.drop_constraint("fk_tool_invocations_rollout_plan", type_="foreignkey")
        batch.drop_column("rollout_plan_item_id")
        batch.drop_column("rollout_plan_id")
    op.drop_table("tool_rollout_plan_counters")
    op.drop_index(
        "ix_tool_rollout_plan_events_created",
        table_name="tool_rollout_plan_lifecycle_events",
    )
    op.drop_table("tool_rollout_plan_lifecycle_events")
    op.drop_index("ix_tool_rollout_items_provider", table_name="tool_rollout_plan_items")
    op.drop_index("ix_tool_rollout_items_tool", table_name="tool_rollout_plan_items")
    op.drop_table("tool_rollout_plan_items")
    op.drop_index("uq_tool_rollout_single_active", table_name="tool_rollout_plans")
    op.drop_index("ix_tool_rollout_plans_window", table_name="tool_rollout_plans")
    op.drop_index("ix_tool_rollout_plans_lifecycle", table_name="tool_rollout_plans")
    op.drop_table("tool_rollout_plans")
